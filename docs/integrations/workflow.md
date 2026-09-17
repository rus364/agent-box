# Task workflow

Enable **workflow** in `agent-box config`, configure tracker access, and run
`ab-setup` in a repository. This installs ten short owned skills:
`ab-tracker`, `ab-setup`, `ab-adopt`, `ab-normalize`, `ab-interview`, `ab-plan`, `ab-work`, `ab-review`,
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

The coordinator is the only writer. It alone writes tracker requirements,
lifecycle state, assignees, relations and dependencies, progress comments and
checkboxes; it alone creates branches, commits and pushes; it alone touches
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
bounded research question the coordinator either spawns a native subagent itself
(its profile sets the model) or launches a managed one:

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
afterwards, so editing the task description, committing or validating while a
delegate works affects no later command.

A managed worker needs the task branch checked out and holds the local work lock,
so validation cannot run under it. A worker failure other than an unavailable CLI
or model never falls back to another candidate, because files may already have
changed. Managed scouts may overlap up to `workflow.scout.limit` (default 3),
counted by per-repository slot locks that a finished or killed process releases;
the coordinator counts its own native scouts. With no candidate configured for the
role, `delegate` refuses and the coordinator runs a native subagent instead.

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

### Reviewer rounds

A review round runs managed or native. `workflow review TASK` runs the first
available reviewer candidate as a managed CLI. `workflow review TASK --native`
writes the same input package and prints `message`, the prompt for a fresh
read-only subagent the coordinator starts without its own history; the
coordinator saves the subagent's final message and records it with
`workflow review TASK --result FILE --provider PROVIDER --model MODEL [--effort EFFORT]`.
The native result is taken on trust: the named model and the freshness of the
context are the coordinator's word, not something workflow can check. Only the
latest round can be answered, and a new round removes the input of an unanswered
native one.

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

Each role owns an ordered candidate list: `workflow.scout`, `workflow.worker` and
`workflow.review` each hold `models`; workers and reviewers also hold optional
`tiers.N` for task tiers 1-3. Scouts have no tiers. A candidate names the CLI that
runs it and what it runs: `provider` (`codex` or `claude`), `model`, `effort`,
`fast` (default `false`) and `api_provider` (empty for the built-in API). Roles
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
fast, remove; the picker offers every CLI's models together), shown in
`config show` in the `--scout` spelling `PROVIDER[@API]:MODEL:EFFORT`, and travel in
the container's minimal workflow configuration. The effort value `default` omits
agent-box's effort override; it does not disable thinking or pass a literal `none`
value to the provider. A new candidate starts at `medium`. Lists start empty
rather than guessing what the account can access.

### Task tiers

A task may carry one `ab:tier:1`, `ab:tier:2` or `ab:tier:3` label. It selects
models only while `workflow.use_tiers` is on (default off): set it globally, in
`.agent-box.json`, or for one session with `--tiers` / `--no-tiers`. Off, every
worker and reviewer runs its role's `models` whatever the task's tier; tier lists
and tier labels stay as they are. When and how a tier is assigned is decided in
ab-plan.

| Worker or reviewer work | Candidates |
| --- | --- |
| Session pin (`--worker SPEC`, `--reviewer SPEC`) | The pinned model only |
| Tiers on, task tier N with a nonempty `tiers.N` | `tiers.N` |
| Otherwise, nonempty `models` | `models` |
| Otherwise | Reviewer: native review only (`review --native`); `delegate` refuses |

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
per CLI for each role's `models` (`ab-scout`, `ab-worker`, `ab-reviewer`) and for
each nonempty worker or reviewer tier list (`ab-worker-N`, `ab-reviewer-N`); each
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
for the task's head and base commits. A new commit or a moved base retires all
three; editing the description, checking items, changing the tier, relations or
hierarchy retires nothing. Move anything important from a discussion into the
task description, since that is what the reviewer reads.

`--brief FILE` is optional for review: a short English note on what the tracker
lacks, such as technical risks or validation limits. Keep it outside the working
tree (`mktemp /tmp/agent-box-review.XXXXXX`) and remove it after the round.
Acceptance operations require a clean tree including untracked files; an untracked in-tree
brief blocks review even when no branch switch is needed.
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
file, outside the handoff limit. The reviewer reads descriptions and diff before the brief.
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
`agent-box workflow inspect TASK` is read-only and works before begin, from another
branch or with uncommitted files. It reports current/found branches, effective
configuration, review budget, acceptance availability/reasons and a next step.
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
before saving so a revoked PASS cannot return. Remote GitHub review requests remain
coordinated separately. Skills stop on ambiguity or no progress and retain human
iteration history in the tracker. Validation is a real command with an observed
exit status; the coordinator does not self-review.

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

It needs successful validation of the same head and completed acceptance checks.
`approve` records the head, actor and reason as human acceptance; it does not
write a reviewer PASS, change policy or mark the task Done. As with reviewer
reports, this is a coordinator contract, not authentication of the human's
identity. A request to avoid delegation alone is not approval. A new commit, a
moved base or a later review round retires the decision. Ownership, checklists,
dependencies, validation and merge proof remain mandatory, and the normal
post-merge cleanup still runs.

If publication or the tracker update fails after the fast-forward, the local merge
and task branch are preserved and the task stays Review. Remain on the base and run
`workflow land PARENT reconcile`. With auto-push enabled it retries publication
of the exact accepted head without repeating the merge; an advanced local base
is refused, and an already-published exact head safely permits retry after a
tracker failure. An explicit auto-push=false permits local-only recovery.
Done reconciliation and cleanup never opportunistically publish the base.
It rereads completion constraints and marks
Done only when the current local task-branch tip is contained in the current
local base **and** the pre-merge acceptance still covers that exact head and base
identity. Before changing Git, landing saves one disposable
acceptance snapshot with the validated scope, original diff size/paths and review
result, including any explicit human acceptance. Recovery applies the current
off/auto/always review policy to that original diff, accepting the original scoped
human decision when present, so advancing the base cannot manufacture acceptance.
A changed task head invalidates acceptance even if the new head is also integrated. Git remains the authority for integration; the snapshot neither
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
Before changing an unfinished parent to Done, reconciliation requires successful
validation and applicable independent review of the merged head.
A merged PR proves integration, not acceptance. Missing
local evidence, including in a new clone, requires fresh `workflow validate`
and ab-review as required from the parent branch, then another reconcile without
another merge. If the base already contains the implementation, auto/always
requires review of the complete implementation using requirements, code and Git
history; an empty diff cannot justify skipping review. Already-Done reconciliation
checks surviving refs and does not write lifecycle.

## Branch cleanup

`workflow.cleanup_local_branches` and `workflow.cleanup_remote_branches` are
independent booleans, both off by default. They are editable in the workflow
config menu and visible in `config show`. With either enabled, landing and
reconciliation attempt cleanup only after proving the merge and successfully
marking the parent Done. Cleanup outcomes appear separately under `cleanup` in
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
In local mode, the parent head must be an ancestor of the local base, and each
descendant head must be an ancestor of the exact accepted parent head supplied
by landing or unfinished-parent reconciliation. Done cleanup retries recover
that head from the existing local acceptance snapshot, checking the task/base
context, original scope, validation and applicable review. This works after
parent refs disappear; a later base or moved parent ref cannot widen the accepted
result. Missing, stale or inconsistent evidence preserves descendants with a
reason, without blocking independent parent cleanup or undoing Done.
Remote deletion additionally requires the candidate head in the
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

`workflow land PARENT cleanup` retries the same configured policy for a Done
parent and its live descendants without merging or writing tracker state.
Remaining descendants are considered even when the parent ref is absent.
Each surviving branch must prove integration again; Done alone is never merge evidence. Missing branches
are safe no-ops, including after a partial cleanup or a title change.
Reconciliation of an already Done parent checks surviving local and live origin
refs independently of cleanup settings, without rewriting tracker state. Each
head must match the exact merged PR in GitHub mode or be integrated into the
local base in local mode. Ambiguous or unmerged refs refuse reconciliation;
current-branch and worktree protections apply only to deletion. If parent
refs are absent, reconciliation reports no merge identity and still attempts
configured descendant cleanup. When all eligible refs are absent, cleanup is a
safe no-op.
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
of a subtask, landing of a parent) and otherwise bounded like receipts: 20 runs and
seven days per checkout.

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

## Fast mode

`fast` is an independent boolean on each candidate of every role list. It defaults to `false`: agent-box adds no
speed override or native capability requirement, preserving existing provider
and session behavior. This does not turn off fast already selected by the user
in the provider. Enable a request through the candidate's **Fast** menu entry.
Fallbacks keep their own request. It changes neither model, effort, provider
selection nor review policy. `fast: true` records a higher-speed request at
higher usage/cost, not delivered service.

Support checked 2026-09-11:

- Codex: exact IDs `gpt-6-astra`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`,
  `gpt-5.6`, `gpt-5.5`, `gpt-5.4`. When requested, CLI and native profiles set
  `service_tier="fast"` and `features.fast_mode=true`. False leaves the original
  CLI/profile fields unchanged. Spark is a separate model, not this setting.
  Sources: [Codex speed](https://learn.chatgpt.com/docs/agent-configuration/speed),
  [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).
  Local Codex 0.154.0 catalog advertises priority/Fast for Astra/Sol/Terra/Luna/5.5.
- Claude: exact IDs `claude-opus-5` and `claude-opus-4-8`. Managed CLI requests
  `claude -p --settings '{"fastMode":true}'`; false adds no settings override.
  Unverified aliases and other models are rejected to prevent automatic model
  switches. Requires account access and usage credits or paid API access;
  third-party cloud backends are unsupported. Source:
  [Claude fast mode](https://code.claude.com/docs/en/fast-mode).
- Codex's priority service tier represents Fast. Claude profiles cannot
  independently pin speed; a native subagent inherits the session's. A native
  review result records no speed, so a configured fast request is delivered only
  by the managed run.

The coordinator's own work and the native subagents it spawns retain their
previous speed behavior. Ordinary interactive launcher commands
keep provider-owned settings and user CLI arguments; workflow matrices do not
change the coordinator's session. GitHub-hosted reviews have no independent
fast control here; use local workflow review for configured speed.

Config apply validates candidates before creating native profiles; invalid fast
combinations disable only intact owned profiles, preserving user edits. The
provider can deny or downgrade a request due to access, limits, policy or CLI
support. Tests prove configuration and request construction, not account
entitlement or acceleration. Config/apply does not change billing settings,
bypass provider checks or run paid fast inference.
