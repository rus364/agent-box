# Tracker

The tracker integration gives agents one command, `agent-box tracker`, for
reading and maintaining tasks in an external tracker, in host and container
sessions alike. Providers are **Vikunja** (API v2), **GitHub Projects**, and
**Jira Cloud / Server / Data Center** (REST v2).

The tracker is the source of truth. Lifecycle comes from the board's own
columns, branches and pull requests from Git and GitHub; agent-box stores no
copy of either, and comments are a human journal with nothing machine-readable
in them. The [workflow](workflow.md) builds on this integration.

## Enabling it

Tracker integration is off and no provider is selected by default. A saved
connection does not select its provider. Choose a global default or let each
repository select its own provider in `.agent-box.json`.

1. In `agent-box config`, enable **Tracker**, choose the provider and fill its
   connection under **connections**: the server URL and a token. Credentials
   stay in the global configuration and reach a session only through its
   private snapshot; see the provider sections below for what each needs.
2. Enable the **workflow** if you want the `ab-*` skills; with the tracker
   alone only `ab-tracker` is installed.
3. Bind each repository to its project with the `ab-setup` skill, or directly:

```bash
agent-box tracker status                           # account, binding, lifecycle columns
agent-box tracker projects                         # projects visible to the token
agent-box tracker setup --project 12               # preview
agent-box tracker setup --project 12 --apply       # save the binding
```

`setup` previews, for every project in scope, the board it resolved and the
lifecycle columns it recognized; `--apply` writes `.agent-box.json` and a
managed block at the start of `AGENTS.md` (and an import into an existing
`CLAUDE.md`, which it never creates). It preserves your other project
overrides and text, and refuses blocks you edited. Commit the binding: a clone
on another machine then needs only a matching global connection.

The binding stores `tracker.provider`, `tracker.project`, `tracker.recursive`
and the connection URL; see
[project configuration](../configuration.md#project-configuration). The global
connection must match that URL.

## Lifecycle

Every bound project needs exactly these five states on its board:

| State | Meaning |
| --- | --- |
| Backlog | Requirements or scheduling are not ready |
| Ready | Specified; dependencies and ownership are checked before starting |
| In progress | Claimed by the token's user and being worked on |
| In review | The parent's work is accepted locally; landing is pending |
| Done | Integrated and validated, or closed with a reason |

Columns are matched by their exact names, ignoring case, and read live on every
command, so a renamed or deleted column becomes an error rather than a misplaced
card. A task whose state cannot be read — a broken board, a card outside every
column — reads as `Unknown`, with `state_reason` saying why.

## Everyday commands

Output is JSON. `get` accepts what a user would type: a visible number such as
`"#21"`, an exact title or a task URL. Other task commands take the canonical
`id` that `get` returns.

```bash
agent-box tracker ready                            # Ready tasks you may claim
agent-box tracker get "#21"                        # full task, with comments
agent-box tracker get 123 --no-comments
agent-box tracker search "login" --include-done    # open tasks; --include-done adds finished ones
agent-box tracker claim 123                        # assign yourself and enter In progress
agent-box tracker checklist 123                    # revision and checklist items
agent-box tracker check 123 1 3 --revision REV     # tick items 1 and 3
agent-box tracker comment 123 --file progress.md   # append a journal comment
agent-box tracker create --title "..." --description-file task.md --parent 120
agent-box tracker relation 124 blocked-by 123
agent-box tracker state 123 Done --reason "Superseded by #130"
```

- **Claiming** checks state, dependencies and assignees, then assigns you and
  moves the task to In progress. Assignment is a cooperative lock, not a
  distributed one: coordinate when several machines share one account.
- **Done** belongs to `agent-box workflow finish`, which proves the work was
  integrated. `state ... Done --reason` closes tasks that were never
  implemented, and refuses while a task's `ab/<id>-` branch still holds commits
  that were not integrated.
- **Priority** is a field, not text: `create --priority N` or
  `update ID --priority N`, with 0 unset, 1 low, 2 medium, 3 high, 4 urgent,
  5 immediate.
- **Relations** are `parent`, `child`, `blocks`, `blocked-by`, `related` and
  `duplicate`. Only blockers gate work: a blocked task cannot be claimed.
- **Search** matches one case-insensitive substring of the title or
  description, not comments; search distinct terms separately.

## Failures and retries

There are no automatic retries. Every error carries a `category` and a
`retryable` hint about the failed request.

- `transport` on a read: retry the read.
- `unknown-outcome`: a write may have been applied. Reread the task (or
  `comments ID`) before writing again; after an uncertain `create`, search by
  title with `--include-done` instead of creating a second task.
- `conflict`: the task changed since it was read, for example a checklist
  revision that moved because someone commented. Read again, then write.

## Vikunja

**Connection:** the server URL and an API token. A loopback URL is reached
from the box through `host.containers.internal`.

**Projects and boards.** A binding names a project ID and may be
`--recursive`, covering every project under it. A new binding is root only;
rerunning `setup` without `--recursive` or `--no-recursive` keeps the saved
scope. With `--recursive` the projects are searched
together and each resolves its own board. Projects outside the subtree stay
readable and are never changed. Each project needs exactly one Kanban view with
manually managed buckets named after the five states; the view's done bucket is
Done. Vikunja's default `To-Do` and `Doing` are not recognized, so add the
missing columns:

```bash
agent-box tracker provider vikunja board init 12 --recursive          # dry run
agent-box tracker provider vikunja board init 12 --recursive --apply
```

Board init only adds columns and sets the done bucket; a column it cannot
recognize, an ambiguous name or a filter-built board stays reported for you to
fix in Vikunja.

**Moving and cleanup.** `tracker move TASK... --project P` moves tasks, each
with its whole subtree, into another project of the binding; IDs stay and
visible numbers change. `tracker delete-project P` deletes an emptied
subproject of the binding, never the root or a project that still holds tasks.

**Product extensions** (`agent-box tracker provider vikunja --help`): project
and label management, board views and buckets, raw task fields, bulk updates,
and structured imports of a CSV or JSON plan. Imports, bulk updates and board
init preview by default and write with `--apply`; the other commands act
directly. An import only creates tasks; afterwards
they are maintained in the tracker, not by editing and re-importing the plan.

**Adopting an existing project.** `ab-adopt` brings a project that already
holds tasks into the workflow without touching the original: it copies the tree
with Vikunja's native duplication, normalizes the copy, audits it, and at
cutover renames the original to `TITLE — backup ID` and binds the repository to
the copy. Each step previews before `--apply`, and an interrupted step is
resumed by rerunning it. The steps share a receipt that `clone` creates in
agent-box's workflow state and reports; `boards` and `cutover` find it on their
own, and ordinary tracker commands work on the staging copy with
`--adoption RECEIPT`:

```bash
agent-box tracker audit --project 12 --recursive
agent-box tracker adopt clone --project 12 --apply     # reports the receipt path
agent-box tracker adopt boards --apply
agent-box tracker --adoption RECEIPT audit
agent-box tracker adopt cutover --apply
```

Inside a workspace box the receipt must stay in workflow state, which outlives
the box; omit `--receipt` to keep the default.

Pause other edits to the project while adopting; there is no incremental sync.
Relations to tasks outside the tree are carried over where Vikunja allows it,
and the report names every one that needs a human decision.

## Jira

Use the Jira site base URL, including its context path when present, without
`/rest/api/2`. Choose `jira` and set its connection in `agent-box config`:

- **Server/Data Center:** `url` and a personal access token in `token`; leave
  `username` empty. Requests use Bearer authentication.
- **Cloud:** `url`, the account email in `username`, and an unscoped/classic
  API token in `token`. Requests use Basic authentication. Scoped gateway
  tokens and OAuth are outside this adapter's scope.

The token and email stay in the global account and private session snapshot;
the repository contains only the URL and project key. Tracker exposure stays
off by default. No MCP server or additional Python dependencies are needed.

```bash
agent-box tracker --provider jira projects
agent-box tracker --provider jira setup --project PROJ
agent-box tracker --provider jira setup --project PROJ --apply
agent-box tracker get PROJ-123
agent-box tracker get '#123'
```

Canonical IDs are native issue keys (`PROJ-123`), never Jira's numeric database
IDs. A visible number resolves within the bound project. Exact titles must be
unique; issue URLs must belong to the configured site and project. Other
projects stay readable by canonical key for dependency inspection, but writes
require the bound project. Jira has no recursive project hierarchy.

Lifecycle comes from **native workflow statuses**, rather than an agile board's
columns. Every issue type in the project must expose the five lifecycle names
above, ignoring case, with one status ID per name shared across issue types.
Additional statuses read as `Unknown`. Setup reports missing/ambiguous statuses;
configure workflows in Jira administration before applying the binding. State
changes require exactly one currently available transition to the requested
status. Jira permissions, screens and validators still apply. Automatic workflow
creation, status renaming and board provisioning are unsupported.

Task creation selects the native `Task` type (or the sole non-subtask type).
`create --parent PROJ-123` selects `Sub-task` (or the sole subtask type) and
creates a native subtask. Existing issues cannot be reparented through this
adapter. Extra required fields without defaults must be configured in Jira.
One assignee is supported; Cloud uses `accountId`, Server/DC uses username.
Descriptions and comments use REST v2 text; unexpected structured descriptions
are refused instead of converted lossily. Checklists remain plain text.

Links match Jira's outward descriptions: `blocks`, `relates to`, `duplicates`.
`blocked-by` reverses `blocks`; incoming block links gate readiness. The link
type must be unique. Duplicate links do not close issues. Tier/normalization
use native labels. Priorities read `Lowest`/`Low` as 1, `Medium` as 2, `High`
as 3, `Highest`/`Urgent` as 4 and `Immediate` as 5; unknown names read as 0.
Writes select Low/Medium/High/Urgent/Immediate, with Highest as the fallback
for Urgent. Creation with 0 keeps Jira's default; updating to 0 requests a
cleared priority and may be rejected by Jira's field requirements.

Project move/delete/adoption, imports, attachments and provider-specific bulk
commands are unsupported. Optimistic rereads detect stale tasks and comments;
they are not atomic compare-and-swap. Lost write replies are never retried
automatically. A partially initialized create reports its issue key so it can
be inspected before creating another issue.

Server/DC was exercised on the authorized local Jira 10.7.4 test instance:
project/task reads, parent/subtask creation, links and removal, tier/labels,
comments, stale-update refusal, checklists, assignment and the full lifecycle
including reopening. Cloud has isolated contract/transport tests against the
official REST v2 interface, including enhanced cursor search; it has **not**
been tested against a live Cloud account. This is not a claim of live Cloud
acceptance. See `tests/test_tracker_jira.py` for offline checks.

## GitHub Projects

**Connection:** the API URL (`https://api.github.com`, or
`https://HOST/api/v3` for GitHub Enterprise Server) and a **classic** token
with the `repo` and `project` scopes, or `gh auth token` with those scopes.
Fine-grained tokens cannot reach projects of a personal account. This token is
separate from **Credentials → GitHub**, and it can also push code, so enabling
the tracker gives the box that access.

**Binding.** A binding names a user or organization project, as `owner/number`
or the project's URL (for example `octo-org/5`), plus this repository, which
is read from origin. GitHub projects have no subprojects, so bindings are never
recursive.

- **Tasks are this repository's issues on the board.** The issue number is
  the task ID. Issues of other repositories on the same board are read-only
  (`owner/repo#N`), useful as blockers. Draft items are not tasks:
  `tracker provider github drafts list` shows them and
  `drafts convert ITEM` turns one into an issue in the same column. Pull
  requests are never tasks.
- **Done is a closed issue.** agent-box moves the card and closes or reopens
  the issue itself; an open issue in the Done column reads as `Unknown` until
  one of the two is fixed. `state ... Done --reason` closes the issue as not
  planned.
- **Relations are GitHub's own:** sub-issues for parent and child,
  issue dependencies for blockers, *relates to*, and *duplicate of* — which
  closes the issue as a duplicate.
- **Priority** is the board's Priority field with the options Immediate,
  Urgent, High, Medium and Low, highest first. Tier and normalization markers
  are repository labels (`ab:tier:N`, `ab:normalized`).
- `move`, `delete-project` and staging adoption (`adopt clone` / `cutover`)
  do not apply. Existing projects are normalized in place, as described below.

**Preparing the board.** Board init plans the Status options (Backlog, Ready,
In progress, In review, Done) and the Priority options. It renames the Board
template's `Todo` to Backlog and `P0`–`P2` to Urgent, High and Medium, keeping
option IDs so existing cards keep their values:

```bash
agent-box tracker provider github board show octo-org/5
agent-box tracker provider github board init octo-org/5            # dry run
agent-box tracker provider github board init octo-org/5 --apply
```

**Turn off two project workflows.** Under the project's **Workflows**, disable
*Pull request linked to issue* and *Pull request merged*: they move cards when
a pull request links or merges, before agent-box has proved anything.
`tracker status` and `setup` warn while either is on. For the same reason
agent-box writes no closing keywords such as `Closes #N` into pull requests and
refuses a pull request body that contains one.

**Adopting an existing project** happens in place: run board init, then
`ab-normalize` to process the tasks already there.

## Normalizing tasks

`ab-normalize` processes tasks in a bound project that have not been handled
yet. A processed task carries the `ab:normalized` label, which means
processed, not Ready or Done; remove it, or run
`tracker normalized ID --undo`, to request another pass.
`tracker normalized --done` marks every completed task in scope at once.

## In the box

An enabled tracker renders its connection into the session's private snapshot;
the box never sees `config.json`. Disabling the tracker removes the connection
secrets from the rendered files even while the workflow stays enabled. How the
contract is implemented — reference resolution, write checks, adoption
receipts — is recorded in the [tracker design notes](../design/tracker.md).
