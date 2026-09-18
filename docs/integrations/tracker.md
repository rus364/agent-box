# Tracker

Enable **tracker** in `agent-box config`, select a provider, and configure its
server URL and API token under **connections**. Vikunja API v2 is implemented;
Jira, Linear and GitHub Issues entries are explicit unsupported-provider stubs.
They fail with an actionable error instead of pretending to synchronize tasks.

The command is `agent-box tracker` in both host and container sessions. There is
no separate skill-owned executable or independent credential configuration. Output
is JSON by default; no `--json` flag. `agent-box tracker status` reads account,
binding and lifecycle columns; `agent-box tracker get ID` reads a full task.
Enable the workflow to install the remaining `ab-*` skills, then use `ab-setup`
to connect a repository to a project.

```bash
agent-box tracker status
agent-box tracker projects
agent-box tracker setup --project 12 --recursive
agent-box tracker setup --project 12 --recursive --apply
agent-box tracker ready
agent-box tracker get "#21"
agent-box tracker get "Exact task title"
agent-box tracker resolve "#21" --project 42
agent-box tracker get 123
agent-box tracker claim 123
agent-box tracker checklist 123
agent-box tracker check 123 1 3 --revision '"1789633065000000000-2"'
agent-box tracker comment 123 --file /tmp/progress.md
agent-box tracker state 123 124 Done --reason 'Superseded by task 130'
agent-box tracker normalized --done --recursive
agent-box tracker move 123 --project 42
agent-box tracker delete-project 42
```

`setup` previews, for every project in scope, the kanban view it resolved and
the lifecycle columns it recognized. It requires five distinct states:
**Backlog, Ready, Doing, Review, Done**, and it does not add columns
automatically. Each project needs exactly one kanban view with manually managed
buckets; the view's own done bucket is taken as Done when it has one, and the
remaining columns are matched by title (`Backlog`/`To-Do`, `Ready`,
`Doing`/`In progress`, `Review`, `Done`). Anything missing, duplicated or
ambiguous is reported per project and blocks `--apply` rather than being
guessed. There is no per-repository override table: a board that does not say
what its columns mean is fixed in the tracker.

Views and column IDs are read live on every run and never stored, so a renamed
or deleted column surfaces as an error instead of silently moving a card. The
same holds for the base branch, which is origin's default branch, and for the
pull request, which is looked up on GitHub by head and base.

With `--recursive` the binding covers the root project **and every project under
it**: those projects are searched together, each resolves its own board, and
tasks in them can be claimed and moved. Projects outside the subtree stay
readable but can never be mutated by this repository's workflow.

Global configuration holds accounts, credentials and integration permissions.
`.agent-box.json` stores `tracker.provider`, `tracker.project`,
`tracker.recursive` and `tracker.connections.<provider>.url`, alongside optional
[project configuration overrides](../../README.md#project-configuration).
A project ID identifies a project on that server; the selected connection URL
must match it. Commit this portable configuration. A fresh clone on another
device needs a matching global connection, or `--tracker URL` with
`VIKUNJA_TOKEN` exported for one session. Changing servers permanently requires
updating the project binding. Setup and adoption
preserve other project overrides. Setup maintains checksum blocks in
`AGENTS.md` and the `CLAUDE.md` import, preserving outside text and refusing
edited blocks, symlinks and ambiguous ownership.

Search results are compact JSON; `get` includes the description, relationships
and comments. `get` accepts the user's reference directly.

`tracker resolve REFERENCE` selects by visible project number (`21` or quoted
`"#21"`), exact title, or a task URL from the configured tracker. It searches the
repository binding, including children when recursive. Numbers use Vikunja’s
`GET /api/v2/projects/{project}/tasks/by-index/{index}` for each scoped project.
Only a missing-task response counts as absence; other API errors stop resolution. One exact match returns
the task; duplicates report project names, numbers, titles and URLs for a user
choice. `--project PROJECT` narrows to that single project inside the binding;
`--title` treats even a numeric reference as a title. Missing matches fail without
falling back to description text or global IDs. URLs outside the selected scope
fail and are never redirected to adopted copies.

`tracker get REFERENCE` applies the same rules and also accepts a canonical ID.
A bare number is looked up both ways: when a canonical ID inside the binding and
a visible number name different tasks, `get` refuses and asks for the quoted
`"#21"` or the task URL instead of guessing. A canonical ID outside the binding
still reads when no visible number matches.

Full and compact tasks expose `number` alongside canonical `id`, project and URL.
Pass that `id` unchanged to mutations and workflow commands. Internal IDs,
relations and branch names retain their existing meaning. Text search only finds
candidates; a substring match does not establish the user's intended task.

Comments are not included in Vikunja's server-side text search. Task IDs resolve
directly; ambiguous project names require an exact path or ID.

Claims check status, dependencies and assignees, assign the authenticated user,
move to Doing, and reread the result. Same-user Doing tasks require deliberate
`--resume`. A local file lock protects simultaneous local claim commands.
Vikunja assignment plus column movement is **not an atomic distributed lock**;
separate machines and sessions using one account require coordination. The adapter rereads and compares the task before editing; changed snapshots
return a conflict for reconciliation, never a blind retry. It also sends ETags
when supplied, but the tested Vikunja version ignores If-Match on PATCH despite
returning ETags on GET. Optimistic checks cannot close the race between the
last read and the write; they are not atomic compare-and-swap.

`checklist` prints the task's revision beside the item indices, and `check
ID INDEX... --revision REVISION` writes every named item into the description
at once. The revision is the same ETag the adapter reads; comparing it before
the write catches a checklist edited since it was read, and it is a client-side
check, not a compare-and-swap, for the same reason ETags cannot be one here. It
belongs to the whole task rather than the description, so posting a progress
comment moves it as well: read `checklist` immediately before `check`.

`Done` belongs to `agent-box workflow finish`, which proves integration.
`state TASK... Done --reason TEXT` is the exception for tasks that were never
implemented: it closes them and journals that reason on each. It refuses while
a task's `ab/<id>-` branch still holds commits neither the base branch nor the
parent task's branch contains, naming the branch and the `workflow finish`
command that would move them, and it judges every named task before moving any
of them. Origin is asked for those branches directly, because another checkout
can advance or even create one and `refs/remotes/*` is only as fresh as the last
fetch; a commit origin reports and this checkout has never fetched is left
unjudged, and the refusal asks for a fetch rather than assuming it landed. `normalized --done` marks every completed task in scope as processed
in one command, because a Done task has nothing left for `ab-normalize` to do.

`move TASK... --project P` moves tasks into another project of the binding,
each with its whole subtask tree, because the provider keeps a parent and its
subtasks in one project; a subtask whose parent stays behind is refused. The
task keeps its ID, relations, labels, comments, priority and assignees, which
the move checks afterwards, but gets a new visible number (rows report
`previous_number`). Vikunja drops a moved card into the target's default column
and ignores a column sent with the project change, so the state read beforehand
is restored by a second write. Between the two writes that state exists nowhere
else, so the move records it in workflow state first; a rerun of the same
command restores what an interruption left, and any other move waits for it.
Moving is only this command: neither `task-fields` nor `bulk-update` accepts `project_id`.
`delete-project P` then deletes an emptied subproject of the binding. The
provider would delete a project's tasks and child projects with it and has no
undo, so a project that still holds either, the binding root and any project
outside the binding are refused. The provider's delete is unconditional, so the
command archives the project, which then refuses new tasks, moved-in tasks and
child projects, checks it is still empty, and only then deletes it; a refusal
unarchives it again.

Comments are a human journal. Nothing machine-readable lives in one: branch,
base branch and pull request come from Git and GitHub, lifecycle from the
tracker's own fields. Tracker content authored by the coordinator follows the
task's language. Fixed diagnostic comments generated automatically by agent-box
may be English; they are stable system explanations rather than user-authored
task content.

Task content uses its language. Markdown checkboxes represent evidenced progress;
only independent review can provide review acceptance. The coordinator is the
only writer of the tracker: requirements, lifecycle, assignees, relations,
progress comments and checkboxes. A worker changes project files and answers
the coordinator; reviewers and scouts only read. That is a contract, not credential
isolation, and it is the same for a native subagent and a managed CLI run.

## Product extensions

Use `agent-box tracker provider vikunja --help`. Common workflow operations stay
in the provider-neutral commands. Extensions cover:

- `projects tree|children|create|update|delete` and `labels list|create|add|remove|delete`;
- `board show|buckets|move` with an explicit project and optional view;
- `task-fields` and `delete-task --force`;
- `bulk-update FILE` (preview by default, `--apply` to write);
- `import validate|plan FILE`, accepting CSV or JSON. `plan` is a dry run
  unless `--apply` is supplied; `--create-root` explicitly permits a missing root.

An import creates tasks once, with dependency and parent relations, labels,
assignees, scheduling fields and bucket placement. A plan ID links rows only
within its file; a row that depends on or belongs under an existing task names
that task's tracker ID. An interrupted import resumes from its local checkpoint,
which is the only link between rows and created tasks: nothing is written into
a task body, and a task title already present in the target project is refused
rather than reused. Once imported, tasks are maintained in the tracker, not by
editing and re-importing the plan. Use normal task creation for workflow planning; there is no
requirement to make a subproject per parent task.

## Adoption of existing projects

`ab-adopt` normalizes a native copy of an existing tree. Source tasks, comments,
attachments, relations and project IDs remain available. It does not plan,
decompose or implement tasks; unresolved requirements stay in Backlog for
`ab-interview`, and future planning belongs to `ab-plan`. Adoption also analyzes
relations across the complete tree, adds evidenced missing dependencies and
corrects mistaken internal links. Similar topics alone do not establish a blocker;
material ambiguity is discussed before changing links.

```bash
agent-box tracker audit --project 12 --recursive
agent-box tracker adopt clone --project 12 --receipt /tmp/adoption.json
agent-box tracker adopt clone --project 12 --receipt /tmp/adoption.json --apply
agent-box tracker adopt boards --receipt /tmp/adoption.json --apply
agent-box tracker --adoption /tmp/adoption.json audit
agent-box tracker --adoption /tmp/adoption.json update 456 --description-file /tmp/task.md
agent-box tracker adopt cutover --receipt /tmp/adoption.json
agent-box tracker adopt cutover --receipt /tmp/adoption.json --apply
```

The receipt records the repository/server, source and new project IDs, intended
names, previous binding and exact intentional internal relation removals; it
contains no tracker snapshot or task lifecycle. Staging `relation ... --remove`
records that intent before the API call, allowing safe retries and excluding only
that edge from source-copy preservation checks. Adding it back clears the
exception before the API call. Other missing internal edges still block cutover.
Keep it until adoption finishes. The staging scope is temporary and does not
change `.agent-box.json`. Ordinary staging commands refuse source/external task
mutations and task creation. Audit reports structural errors separately from
preparation warnings: a Backlog task without enough evidence for acceptance or a
tier does not need fabricated requirements to pass adoption.

Cutover checks the copy and audit, prepares managed repository instructions,
renames the original root to `TITLE — backup ID`, renames staging to `TITLE`, then
atomically writes the new recursive binding. If interrupted, rerun cutover with
the same receipt: actual names and binding determine which steps remain. No
tracker objects are deleted, and no tracker restoration is attempted. Concurrent
source/staging editing must be paused for adoption; there is no incremental sync.
A changing source remains usable but the copy can become stale.

A clone interrupted before normalization can be resumed with the same command.
A `clone.pending` project marks an uncertain duplicate POST result. Inspect the
provider, enter the verified new ID into `clone.projects` under that source ID,
and clear `clone.pending` only once verified. If no copy exists, clear the pending
entry only after confirming the request did not commit. Never infer a successful
copy from a similar title or automatically retry an uncertain POST. Do not rerun
clone after normalization: audit staging instead. Incomplete staging projects
remain for inspection; removal is an explicit separate operation.

Vikunja v2.5.0 uses `POST /api/v2/projects/{id}/duplicate` once per project,
with `duplicate_shares: true` and the corresponding new parent. The adapter
matches tasks by their preserved, unique per-project index, adds missing
assignments after native shares exist, and connects relations between copied
projects. Internal relations, comments, attachments/files, views/buckets and
positions use native copying. No custom task/file deep-copy engine is involved.

Collapsing subprojects is optional and happens only when the user asks for it:
`tracker --adoption PATH move TASK... --project P` moves staging tasks between
staging projects and refuses any target outside staging. A moved task gets a new
visible number, which would break the per-project index pairing, so before the
first move the receipt saves `clone.tasks`, the source-to-staging task mapping,
while the indexes still pair up. From then on clone verification and cutover use
that mapping instead, and `clone --apply` refuses to run again.
`tracker --adoption PATH delete-project P` deletes an emptied staging
subproject. After the same checks, and before the delete, it saves the mapping
if no move has, and records the source project in `clone.removed`; clone
verification and cutover then treat that copy as intentionally gone. A copy
recorded as removed that still exists, because the delete was interrupted,
blocks cutover until the command is rerun.

Labels are still global shared objects: adoption may create missing `ab:tier:*`
labels and edit staging attachments, but never rename/delete global labels.
Shares are copied immediately, without a separate permissions
lifecycle. Link hashes, project/task IDs and task UIDs change. Ownership follows
the provider's creating-user rules. Complete history, webhooks, reactions and
all subscriptions are not migrated. Inspect view filters and embedded links for
old IDs. Native copying can skip missing attachment files; attachment/comment
counts and assignment preservation are checked before normalization and again
before cutover, along with preservation of internal source relations. Added
journal comments are allowed; cutover cannot conceal an interrupted internal copy.

External relations are best effort. Clone and cutover add missing `related`,
`duplicateof`/`duplicates`, `precedes`/`follows` and incoming `blocked` relations
from staging. Vikunja adds the inverse on the external task; it requires read
access there and write access on staging. Ordering is preserved as metadata;
agent-box enforces only blockers, with its usual dependency checks. Outgoing
`blocking`, external parent/subtask, historical `copiedfrom`/`copiedto` and
unknown kinds are skipped for a human decision. If already present on staging,
they are reported as `manual` with the same policy warning, never as an approved
automatic transfer. Source/backup relations stay intact.

Skipped or failed external edges are warnings, not cutover blockers. The report
names source, staging, relation kind, external task and the reason. A short
staging comment gives options specific to the relation type. These comments are
fixed agent-box diagnostics and may be English regardless of the task language;
the coordinator still explains the warnings to the user in the user's language.
Eligible relations may need access or manual addition; unsafe types require
agreement on workflow, hierarchy or provenance, another suitable type, or
keeping the original alone. Failure to post that comment is reported as
`comment_warning` for the current apply attempt. Read-only reports, including
repeated completed cutover, do not check journal delivery or reconstruct
previous comment failures; retain the apply output if that diagnostic is needed.
Comments never determine resolution; identical journal entries are reused only
to avoid repeated explanations. Keep work requiring clarification in Backlog; a
missing `related` alone does not reset its status. Ordinary staging CLI writes
remain restricted to staging endpoints and relations.

Before switching binding, cutover retries eligible missing external edges and
rechecks the staging audit. Lost write responses are checked against live tasks.
An API outage or inability to read staging still stops cutover. After binding
switches, rerunning cutover only reads the current report. No relation progress
or resolution flags are stored in the receipt. The ordinary workflow does not
track omitted adoption relations after cutover; the report and comments explain
those omissions to people. Ambiguous boards and invalid staging hierarchy remain
structural errors, including external or unreadable children of staging parents.

Structured imports remain independent. To import into a copied project, use
`tracker provider vikunja import plan FILE --project NEW_ROOT_ID` (and `--apply`
after review). The plan's root name denotes that explicit target even if staging
has a different title or an external parent. Tracker references resolve only
within its subtree, and checkpoints are separated by server/target ID.

Run `PYTHONPATH=. python3 tests/live_vikunja_adoption.py --run` from the checkout
for the opt-in live contract check. It creates and removes its own test projects,
label and attachments; it never accepts an existing project as a target. The
offline suite does not run it.

## Container access

An enabled tracker renders connections and runtime policy into an immutable
`runtime/tracker/workspaces/<id>/sessions/<session>/config.json` snapshot
(session directory 0700, file 0600). The container receives only its workspace's
snapshot directory read-only, not the global config file or snapshots from host
launches or other workspaces. Sessions in the same box share access to these files.
Local workflow evidence, locks, counters and import checkpoints have their own
read-write state mount. Disabling tracker removes connection secrets from the
rendered artifact even when workflow remains enabled.

Loopback addresses become `host.containers.internal`. A DNS name resolving to
the host retains its URL for TLS and virtual-host routing and receives a Podman
host mapping. Remote URLs stay unchanged. Secrets never enter Podman argv.

## Incremental normalization

Use `ab-normalize` for unprocessed tasks in an already bound project. It selects
candidates with `tracker search --exclude-label ab:normalized`; exclusion happens
before CLI output, after the adapter has read all result pages. This limits agent
context, not necessarily API traffic. Compact unfiltered searches remain available
to discover links to previously processed tasks.

`tracker normalized ID` adds the shared `ab:normalized` label only after edits,
journal writes and verification finish. Adoption uses the same marker. It means
processed, not Ready or Done: unresolved decisions can remain explicit in Backlog.
Remove the label in the tracker UI or with `tracker normalized ID --undo` to request
another pass. Other labels are preserved; edits do not automatically reset it.

Priority uses the existing scale: 0 unset, 1 low, 2 medium, 3 high, 4 urgent,
5 immediate. `tracker create --priority N` and `tracker update ID --priority N`
store an assessment grounded in urgency, deadlines and consequences of delay.
