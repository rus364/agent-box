# Tracker

Enable **tracker** in `agent-box config`, select a provider, and configure its
server URL and API token under **connections**. Vikunja API v2 is implemented;
Jira, Linear and GitHub Issues entries are explicit unsupported-provider stubs.
They fail with an actionable error instead of pretending to synchronize tasks.

The command is `agent-box tracker` in both host and container sessions. There is
no separate skill-owned executable or independent credential configuration.
Enable the workflow to install the remaining `ab-*` skills, then use `ab-setup`
to connect a repository to a project.

```bash
agent-box tracker status
agent-box tracker projects
agent-box tracker setup --project 12 --recursive
agent-box tracker setup --project 12 --recursive --apply
agent-box tracker ready
agent-box tracker get 123
agent-box tracker claim 123
agent-box tracker checklist 123
agent-box tracker check 123 1 --text 'Expected acceptance item'
agent-box tracker comment 123 --file /tmp/progress.md
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

Global configuration holds connections, secrets and workflow policy.
`.agent-box.json` holds only provider, server, project and whether the binding
is recursive. `server` stays because a project ID is meaningless without the
instance it belongs to, and Vikunja publishes no stable instance identity of its
own. Commit this portable binding. A fresh
clone on another device requires only the matching global connection, not
local absolute paths or credentials in Git. Setup also maintains checksum
blocks in `AGENTS.md` and the `CLAUDE.md` import, preserving text outside them
and refusing edited blocks, symlinks and ambiguous ownership.

Search results are compact JSON; `get` includes the description, relationships
and comments. Search titles/descriptions first, then read relevant history.
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

Comments are a human journal. Nothing machine-readable lives in one: branch,
base branch and pull request come from Git and GitHub, lifecycle from the
tracker's own fields.

Task content uses its language. Markdown checkboxes represent evidenced progress;
only independent review can provide review acceptance. The coordinator is the
only writer of the tracker: requirements, lifecycle, assignees, relations,
progress comments and checkboxes. A worker changes project files and reports
back; reviewers and scouts only read. That is a contract, not credential
isolation, and it is the same for a native subagent and a managed CLI run.

## Product extensions

Use `agent-box tracker provider vikunja --help`. Common workflow operations stay
in the provider-neutral commands. Extensions cover:

- `projects tree|children|create|update|delete` and `labels list|create|add|remove|delete`;
- `board show|buckets|move` with an explicit project and optional view;
- `external`, `task-fields`, and `delete-task --force`;
- `bulk-update FILE` (preview by default, `--apply` to write);
- `import validate|plan|verify FILE`, accepting CSV or JSON. `plan` is a dry run
  unless `--apply` is supplied; `--create-root` explicitly permits a missing root.

Imports retain stable external identifiers, dependency and parent relations,
labels, assignees, scheduling fields, bucket placement, conflict detection,
resumable state and post-apply verification. They do not become a second task
source of truth. Use normal task creation for workflow planning; there is no
requirement to make a subproject per parent task.

## Adoption of existing projects

`ab-adopt` normalizes a native copy of an existing tree. Source tasks, comments,
attachments, relations and project IDs remain available. It does not plan,
decompose or implement tasks; unresolved requirements stay in Backlog for
`ab-interview`, and future planning belongs to `ab-plan`.

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
names and previous binding; it contains no tracker snapshot or task lifecycle.
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
unknown kinds are skipped for a human decision. Source/backup relations stay intact.

Skipped or failed external edges are warnings, not cutover blockers. The report
names source, staging, relation kind, external task and the reason. A short
staging comment suggests checking access, adding a relation manually, agreeing
on another type or leaving the original relation alone. Failure to post that
comment is reported too. Comments never determine resolution; identical journal
entries are reused only to avoid repeated explanations. Keep work requiring
clarification in Backlog; a missing `related` alone does not reset its status.
Ordinary staging CLI writes remain restricted to staging endpoints and relations.

Before switching binding, cutover retries eligible missing external edges and
rechecks the staging audit. Lost write responses are checked against live tasks.
An API outage or inability to read staging still stops cutover. After binding
switches, rerunning cutover only reads the current report. No relation progress
or resolution flags are stored in the receipt. The ordinary workflow does not
track omitted adoption relations after cutover; the report and comments explain
those omissions to people. Ambiguous boards and invalid staging hierarchy remain
structural errors.

Structured imports remain independent. To re-import into a copied project, use
`tracker provider vikunja import plan FILE --project NEW_ROOT_ID` (and `--apply`
after review); `verify` accepts the same scope. The plan's root name denotes that
explicit target even if staging has a different title or an external parent.
External IDs resolve only within its subtree, and checkpoints are separated by
server/target ID. Without `--project`, global marker ambiguity is still refused.

Run `PYTHONPATH=. python3 tests/live_vikunja_adoption.py --run` from the checkout
for the opt-in live contract check. It creates and removes its own test projects,
label and attachments; it never accepts an existing project as a target. The
offline suite does not run it.

## Container access

An enabled tracker renders only tracker connections and necessary workflow
policy into a private `runtime/tracker/config.json` (directory 0700, file 0600).
The container receives this artifact read-only, not the global config file.
Local workflow evidence, locks, counters and import checkpoints have their own
read-write state mount. Disabling tracker removes connection secrets from the
rendered artifact even when workflow remains enabled.

Loopback addresses become `host.containers.internal`. A DNS name resolving to
the host retains its URL for TLS and virtual-host routing and receives a Podman
host mapping. Remote URLs stay unchanged. Secrets never enter Podman argv.
