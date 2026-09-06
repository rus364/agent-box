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
- `snapshot`, `external`, `task-fields`, and `delete-task --force`;
- `bulk-update FILE` (preview by default, `--apply` to write);
- `import validate|plan|verify FILE`, accepting CSV or JSON. `plan` is a dry run
  unless `--apply` is supplied; `--create-root` explicitly permits a missing root.

Imports retain stable external identifiers, dependency and parent relations,
labels, assignees, scheduling fields, bucket placement, conflict detection,
resumable state and post-apply verification. They do not become a second task
source of truth. Use normal task creation for workflow planning; there is no
requirement to make a subproject per parent task.

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
